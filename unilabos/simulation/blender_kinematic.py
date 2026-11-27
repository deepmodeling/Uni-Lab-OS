"""Blender 无求解器场景：关节姿态、对象变换及可逆父子跟随。"""
import bpy
from mathutils import Matrix


class Scene:
    """持有本会话场景对象；领域负责轨迹、几何和吸附前置条件。"""
    def __init__(self):
        """清空启动场景并拒绝遗留刚体求解世界。"""
        bpy.ops.object.select_all(action='SELECT')
        bpy.ops.object.delete(use_global=False)
        if bpy.context.scene.rigidbody_world is not None:
            raise ValueError('kinematic_rigidbody_world_forbidden')
        self.objects={}

    def create(self, name, matrix, size=None):
        """创建坐标对象或明确尺寸的立方代理，不创建刚体或碰撞器。"""
        if name in self.objects:
            raise ValueError('kinematic_duplicate_object')
        if size is None:
            obj=bpy.data.objects.new(name,None)
            bpy.context.collection.objects.link(obj)
            obj.empty_display_type='ARROWS'
            obj.empty_display_size=.03
        else:
            bpy.ops.mesh.primitive_cube_add(size=1)
            obj=bpy.context.object
            obj.name=name
            # 尺寸烘入网格，使变换和吸附矩阵不携带比例。
            for vertex in obj.data.vertices:
                vertex.co.x*=size[0];vertex.co.y*=size[1];vertex.co.z*=size[2]
        self.objects[name]=obj
        self.set_pose(name,matrix)
        return obj

    def import_obj(self,path,name,parent,local_matrix):
        """加载既有 OBJ 视觉网格并绑定父坐标；不读取或创建碰撞／质量配置。"""
        bpy.ops.object.select_all(action='DESELECT')
        bpy.ops.wm.obj_import(filepath=str(path),forward_axis='Y',up_axis='Z')
        imported=list(bpy.context.selected_objects)
        if not imported:raise ValueError('kinematic_visual_empty')
        for index,obj in enumerate(imported):
            key=f'{name}/{index}'
            if key in self.objects:raise ValueError('kinematic_duplicate_visual')
            obj.name=key
            obj.parent=self.objects[parent]
            obj.matrix_parent_inverse=Matrix.Identity(4)
            obj.matrix_basis=Matrix(local_matrix)@obj.matrix_basis
            self.objects[key]=obj
        self.update()

    def set_pose(self,name,matrix):
        """写入世界变换；调用 update 后才能读取依赖图确认结果。"""
        self.objects[name].matrix_world=Matrix(matrix)

    def update(self):
        """只求值对象依赖图，拒绝场景在运行中被加入刚体世界。"""
        if bpy.context.scene.rigidbody_world is not None or any(o.rigid_body for o in self.objects.values()):
            raise ValueError('kinematic_rigidbody_forbidden')
        bpy.context.view_layer.update()

    def pose(self,name):
        """从已求值 Blender 对象读取世界矩阵，不回传命令缓存。"""
        obj=self.objects[name].evaluated_get(bpy.context.evaluated_depsgraph_get())
        return [list(row) for row in obj.matrix_world]

    def attach(self,child,parent):
        """保持当前世界姿态建立父子关系；已有父级时拒绝重绑。"""
        obj=self.objects[child]
        if obj.parent is not None:
            raise ValueError('kinematic_already_attached')
        world=obj.matrix_world.copy()
        obj.parent=self.objects[parent]
        obj.matrix_parent_inverse=Matrix.Identity(4)
        obj.matrix_basis=obj.parent.matrix_world.inverted()@world
        self.update()

    def release(self,child):
        """解除父级并保留已求值世界姿态，不跳到放置目标。"""
        obj=self.objects[child]
        if obj.parent is None:
            raise ValueError('kinematic_not_attached')
        world=Matrix(self.pose(child))
        obj.parent=None
        obj.matrix_world=world
        self.update()

    def parent(self,name):
        """返回真实 Blender 父对象名，未附着时返回 None。"""
        obj=self.objects[name]
        return obj.parent.name if obj.parent else None

    def save(self,path):
        """保存可复核原生场景；不推进帧或执行刚体烘焙。"""
        bpy.ops.wm.save_as_mainfile(filepath=str(path))
