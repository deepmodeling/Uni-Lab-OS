/* lab_* 扩展：接触流、几何射线、惯量/碰撞对覆盖。
 *
 * 这些函数原先由 Blender 进程内的编译单元提供；独立库重新实现同样的语义，
 * 输出统一用 double，方便 Python ctypes 直接读取。 */

#include "rb_internal.h"

#include <cmath>

int lab_scalar_bytes(void)
{
  return static_cast<int>(sizeof(btScalar));
}

int lab_abi_version(void)
{
  /* 结构体布局或绑定表变化时递增，加载器用它拒绝陈旧缓存。 */
  return 1;
}

int lab_contacts(rbDynamicsWorld *world, rbContact *out, int capacity)
{
  int count = 0;
  btDispatcher *dispatcher = world->dispatcher;
  const int manifolds = dispatcher->getNumManifolds();
  for (int i = 0; i < manifolds; ++i) {
    btPersistentManifold *manifold = dispatcher->getManifoldByIndexInternal(i);
    const int points = manifold->getNumContacts();
    for (int j = 0; j < points; ++j) {
      if (count >= capacity) {
        return -count - 1;
      }
      const btManifoldPoint &point = manifold->getContactPoint(j);
      rbContact &contact = out[count++];
      contact.a = manifold->getBody0()->getUserPointer();
      contact.b = manifold->getBody1()->getUserPointer();
      for (int k = 0; k < 3; ++k) {
        contact.position[k] = static_cast<double>(point.getPositionWorldOnB()[k]);
        contact.normal[k] = static_cast<double>(point.m_normalWorldOnB[k]);
      }
      contact.separation = static_cast<double>(point.getDistance());
      contact.impulse = static_cast<double>(point.getAppliedImpulse());
    }
  }
  return count;
}

void lab_friction_2d(rbDynamicsWorld *world)
{
  world->dynamics_world->getSolverInfo().m_solverMode |= SOLVER_USE_2_FRICTION_DIRECTIONS;
}

void lab_inertia(rbRigidBody *body, double mass, double x, double y, double z)
{
  body->body->setMassProps(static_cast<btScalar>(mass),
                           btVector3(static_cast<btScalar>(x),
                                     static_cast<btScalar>(y),
                                     static_cast<btScalar>(z)));
  body->body->updateInertiaTensor();
}

void lab_ignore(rbRigidBody *body_a, rbRigidBody *body_b, int ignore)
{
  const bool ignore_pair = ignore != 0;
  body_a->body->setIgnoreCollisionCheck(body_b->body, ignore_pair);
  body_b->body->setIgnoreCollisionCheck(body_a->body, ignore_pair);
}

void lab_force_at(rbRigidBody *body, const double *force, const double *point)
{
  const btVector3 world_force(static_cast<btScalar>(force[0]),
                              static_cast<btScalar>(force[1]),
                              static_cast<btScalar>(force[2]));
  const btVector3 world_point(static_cast<btScalar>(point[0]),
                              static_cast<btScalar>(point[1]),
                              static_cast<btScalar>(point[2]));
  body->body->applyForce(world_force, world_point - body->body->getCenterOfMassPosition());
}

void lab_shape_aabb(rbCollisionShape *shape, double min[3], double max[3])
{
  btVector3 aabb_min, aabb_max;
  shape->cshape->getAabb(btTransform::getIdentity(), aabb_min, aabb_max);
  for (int i = 0; i < 3; ++i) {
    min[i] = static_cast<double>(aabb_min[i]);
    max[i] = static_cast<double>(aabb_max[i]);
  }
}

void *lab_ray(rbDynamicsWorld *world,
              const double *from,
              const double *to,
              void *exclude,
              double *distance)
{
  const btVector3 start(static_cast<btScalar>(from[0]),
                        static_cast<btScalar>(from[1]),
                        static_cast<btScalar>(from[2]));
  const btVector3 end(static_cast<btScalar>(to[0]),
                      static_cast<btScalar>(to[1]),
                      static_cast<btScalar>(to[2]));
  btCollisionWorld::AllHitsRayResultCallback callback(start, end);
  world->dynamics_world->rayTest(start, end, callback);

  void *found = nullptr;
  *distance = 1e9;
  for (int i = 0; i < callback.m_collisionObjects.size(); ++i) {
    void *identity = callback.m_collisionObjects[i]->getUserPointer();
    const double hit_distance = static_cast<double>(callback.m_hitFractions[i]) *
                                static_cast<double>((end - start).length());
    /* 与 Blender 侧桥一致：排除指定刚体，只接受 [0.005, 0.1] 米内的最近命中。 */
    if (identity != exclude && hit_distance >= 0.005 && hit_distance <= 0.1 &&
        hit_distance < *distance) {
      found = identity;
      *distance = hit_distance;
    }
  }
  return found;
}
